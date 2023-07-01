import os
import shutil
from threading import get_ident, Thread, Lock

from celaut_framework.dependency_manager.dependency_manager import DependencyManager
from celaut_framework.dependency_manager.service_interface import ServiceInterface
from celaut_framework.protos import celaut_pb2 as celaut
from grpcbigbuffer.client import client_grpc
from grpcbigbuffer.utils import WITHOUT_BLOCK_POINTERS_FILE_NAME

from protos import api_pb2, api_pb2_grpc, solvers_dataset_pb2
from src.envs import SHA3_256_ID, LOGGER, SHA3_256, RANDOM_SHA3_256
from src.regresion import regresion
from src.solve import _solve
from src.utils.singleton import Singleton


class Session(metaclass=Singleton):

    def __init__(self,
                 save_train_data: int,
                 train_solvers_timeout: int,
                 time_for_each_regression_loop: int
                 ):

        # set used envs on variables.
        self.REFRESH = save_train_data
        self.TRAIN_SOLVERS_TIMEOUT = train_solvers_timeout

        self.service: ServiceInterface = DependencyManager().add_service(
            service_hash=RANDOM_SHA3_256,
            config=celaut.Configuration(),
            stub_class=api_pb2_grpc.RandomStub,
            dynamic=False
        )

        self.thread = None
        self.solvers_dataset = solvers_dataset_pb2.DataSet()
        self.solvers = []  # Lista de los solvers por hash.
        self.solvers_dataset_lock = Lock()  # Se usa al añadir un solver y durante cada iteracion de entrenamiento.
        self.solvers_lock = Lock()  # Se uso al añadir un solver ya que podrían añadirse varios concurrentemente.
        self.do_stop = False

        self._solver = None  # Using singleton pattern.
        self._regression = None  # Using singleton pattern.
        self.time_for_each_regression_loop: int = time_for_each_regression_loop

        # Random CNF Service.
        self.random_config = celaut.Configuration()

    def stop(self):
        # Para evitar que la instrucción stop mate el hilo durante un entrenamiento,
        #  y deje instancias fuera de pila y por tanto servicios zombie, el método stop 
        #  emite el mensaje de que se debe de parar el entrenamiento en la siguiente vuelta, 
        #  dando uso de do_stop=True, y a continuación espera a que el init salga del while 
        #  con thread.join().
        if not self.do_stop and self.thread:
            LOGGER('Stopping train.')
            self.do_stop = True
            self.thread.join()
            self.do_stop = False
            self.thread = None

    def load_solver(self, service_with_meta_dir: str) -> str:
        if not self._solver:
            self._solver = _solve.Session()

        service_with_meta = api_pb2.ServiceWithMeta()
        try:
            with open(os.path.join(service_with_meta_dir, WITHOUT_BLOCK_POINTERS_FILE_NAME), 'rb') as f:
                service_with_meta.ParseFromString(f.read())
        except Exception:
            raise Exception('UploadSolver error: this is not a ServiceWithMeta message. ' + str(service_with_meta_dir))

        solver_hash: str = None
        metadata = service_with_meta.meta
        solver = service_with_meta.service
        for h in metadata.hashtag.hash:
            if h.type == SHA3_256_ID:
                solver_hash = h.value.hex()

        if not solver_hash:
            raise Exception('Error loading solver: Solver without hash.')
            # TODO Usar método para extraer el identificador de un servicio del registro.

        with self.solvers_lock:
            if solver_hash and solver_hash not in self.solvers:
                self.solvers.append(solver_hash)

                shutil.move(service_with_meta_dir,
                            os.path.join(DependencyManager().dynamic_service_directory, solver_hash))

                # En este punto se pueden crear varias versiones del mismo solver,
                #  con distintas variables de entorno.
                with self.solvers_dataset_lock:
                    p = solvers_dataset_pb2.SolverWithConfig()
                    p.definition.CopyFrom(solver)
                    p.meta.CopyFrom(metadata)
                    # p.enviroment_variables (Usamos las variables de entorno por defecto).
                    solver_with_config_hash = SHA3_256(
                        value=p.SerializeToString()
                    ).hex()  # This service not touch metadata, so it can use the hash for id.
                    self.solvers_dataset.data[solver_with_config_hash].CopyFrom(solvers_dataset_pb2.DataSetInstance())
                    self._solver.add_solver(
                        solver_with_config=p,
                        solver_config_id=solver_with_config_hash,
                        solver_hash=solver_hash
                    )

        return solver_hash

    def clear_dataset(self) -> None:
        self.solvers_dataset_lock.acquire()
        for solver in self.solvers_dataset.data.values():
            solver.ClearField('data')
        self.solvers_dataset_lock.release()

    def random_cnf(self) -> api_pb2.Cnf:
        while True:
            instance = self.service.get_instance()

            try:
                LOGGER('Generate new random CNF.')
                return next(client_grpc(
                    method=instance.stub.RandomCnf,
                    indices_parser=api_pb2.Cnf,
                    partitions_message_mode_parser=True,
                    timeout=self.service.sc.timeout
                ))

            except Exception as e:
                instance.compute_exception(e)

            finally:
                self.service.push_instance(instance)

    @staticmethod
    def is_good(cnf, interpretation):
        def good_clause(_clause, _interpretation):
            for var in _clause.literal:
                for i in _interpretation.variable:
                    if var == i:
                        return True
            return False

        for clause in cnf.clause:
            if not good_clause(clause, interpretation):
                return False
        return True

    @staticmethod
    def updateScore(cnf, solver: solvers_dataset_pb2.DataSetInstance, score):
        num_clauses, num_literals = (
            len(cnf.clause),
            0,
        )
        for clause in cnf.clause:
            for literal in clause.literal:
                if abs(literal) > num_literals:
                    num_literals = abs(literal)
        type_of_cnf = str(num_clauses) + ':' + str(num_literals)
        if type_of_cnf not in solver.data:
            solver.data[type_of_cnf].index = 1
            solver.data[type_of_cnf].score = 0
        solver.data[type_of_cnf].score = (solver.data[type_of_cnf].score * solver.data[type_of_cnf].index + score) / (
                solver.data[type_of_cnf].index + 1)
        solver.data[type_of_cnf].index = solver.data[type_of_cnf].index + 1

    def start(self):
        if self.thread or self.do_stop: return None
        try:
            self.thread = Thread(target=self.init, name='Trainer')
            self.thread.start()
        except RuntimeError:
            LOGGER('Error: train thread was started and have an error.')

    def init(self):
        if not self._solver: 
            self._solver = _solve.Session()
        if not self._regression: 
            self._regression = regresion.Session(self.time_for_each_regression_loop)

        LOGGER('Init trainer, THREAD IS ' + str(get_ident()))
        refresh = 0
        timeout = self.TRAIN_SOLVERS_TIMEOUT

        # Si se emite una solicitud para detener el entrenamiento el hilo
        #  finalizará en la siguiente iteración.
        while not self.do_stop:
            if refresh < self.REFRESH:
                LOGGER('REFRESH ES MENOR')
                refresh = refresh + 1
                cnf = self.random_cnf()
                LOGGER('OBTENIDO NUEVO CNF. ')
                is_insat = True  # En caso en que se demuestre lo contrario.
                insats = []  # Solvers que afirman la insatisfactibilidad junto con su respectivo tiempo.
                LOGGER('VAMOS A PROBAR LOS SOLVERS')
                self.solvers_dataset_lock.acquire()
                for _hash, solver_data in self.solvers_dataset.data.items():
                    if self.do_stop: 
                        break
                    LOGGER('SOLVER --> ' + str(_hash))
                    try:
                        interpretation, time = self._solver.cnf(cnf=cnf, solver_config_id=_hash, timeout=timeout)
                    except Exception as e:
                        LOGGER('INTERNAL ERROR SOLVING A CNF ON TRAIN ' + str(e))
                        interpretation, time = None, timeout
                        pass
                    # Durante el entrenamiento, si ha ocurrido un error al obtener un cnf se marca como insatisfactible,
                    # tras muchas iteraciones no debería suponer un problema en el tensor.
                    if not interpretation or not interpretation.satisfiable or len(interpretation.variable) == 0:
                        insats.append({
                            'solver': solver_data,
                            'time': time
                        })
                    else:
                        if self.is_good(cnf, interpretation):
                            is_insat = False
                        else:
                            pass
                        if time == 0:
                            score = +1
                        else:
                            score = float(-1 / time)
                        self.updateScore(
                            cnf=cnf,
                            solver=solver_data,
                            score=score
                        )
                self.solvers_dataset_lock.release()
                # Registra los solvers que afirmaron la insatisfactibilidad en caso en que ninguno
                #  haya demostrado lo contrario.
                for d in insats:
                    self.updateScore(
                        cnf=cnf,
                        solver=d['solver'],
                        score=(float(+1 / d['time']) if d['time'] != 0 else 1) if is_insat
                        else (float(-1 / d['time']) if d['time'] != 0 else -1)
                        # comprueba is_insat en cada vuelta, cuando no es necesario, pero el codigo queda más limpio.
                    )
            else:
                LOGGER('ACTUALIZA EL DATASET')
                refresh = 0
                self._regression.add_data(new_data_set=self.solvers_dataset)
                # No formatear los datos cada vez provocaría que el regresion realizara equívocamente la media, pues 
                # estaría contando los datos anteriores una y otra vez.
                self.clear_dataset()
