from dataclasses import asdict, dataclass, field
import configparser
import inspect
import enterprise.signals.parameter  # this is used but only implicitly
import enterprise_extensions.models
import collections.abc
import pickle, json
import importlib
import numpy as np
from enterprise.signals import signal_base


def get_default_args_types_from_function(func):
    """
    Given function, returns two dictionaries with default values and types
    code modified from: https://stackoverflow.com/questions/12627118/get-a-function-arguments-default-value
    """
    signature = inspect.signature(func)
    defaults = {}
    types = {}
    for k, v in signature.parameters.items():
        if v.default is not inspect.Parameter.empty:
            defaults[k] = v.default

        if v.annotation is inspect.Parameter.empty:
            print(f"Warning! {v} does not have an associated type annotation")
        else:
            types[k] = v.annotation
    return defaults, types


def update_dictionary_with_subdictionary(d, u):
    """
    Updates dictionary d with preference for contents of dictionary u
    code taken from: https://stackoverflow.com/questions/3232943/update-value-of-a-nested-dictionary-of-varying-depth
    """
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = update_dictionary_with_subdictionary(d.get(k, {}), v)
        else:
            d[k] = v
    return d


@dataclass()
class RunSettings:
    """
    Class for keeping track of enterprise model run settings
    TODO: link to examples of how to use
    """
    config_file: str = None
    pulsar_pickle: str = None
    noise_dict_json: str = None

    # dictionary of functions that create signals
    signal_creating_functions: dict = field(default_factory=dict)
    signal_creating_function_parameters: dict = field(default_factory=dict)
    # dictionary of functions that create pta objects
    pta_creating_function_parameters: dict = field(default_factory=dict)
    pta_creating_functions: dict = field(default_factory=dict)

    custom_classes: dict = field(default_factory=dict)
    custom_function_return: dict = field(default_factory=dict)

    psrs: list = field(default_factory=list)
    noise_dict: dict = field(default_factory=dict)
    sections: dict = field(default_factory=dict)
    typed_sections: dict = field(default_factory=dict)

    def update_from_file(self, config_file: str) -> None:
        """
        Set defaults for functions from file
        """
        config = configparser.ConfigParser(comment_prefixes=';',
                                           interpolation=configparser.ExtendedInterpolation())
        config.optionxform = str
        config.read(config_file)
        exclude_keys = ['function', 'module', 'class', 'signal_return', 'pta_return', 'custom_return']
        for section in config.sections():
            config_file_items = dict(config.items(section))
            self.sections[section] = config_file_items
            if section == 'input' or section == 'output' or section == 'DEFAULT':
                # read in input / output files
                for item in config_file_items.copy():
                    if not config_file_items[item]:
                        config_file_items.pop(item)
                self.update_from_dict(**config_file_items)

            elif 'class' in config_file_items.keys():
                """
                Initialize a class given in a config file 
                """
                # Import a module defined elsewhere
                module = importlib.import_module(config_file_items['module'])

                # import a class from a module
                custom_class = getattr(module, config_file_items['class'])

                class_parameters, types = get_default_args_types_from_function(custom_class.__init__)
                class_parameters_from_file = self.apply_types(config_file_items, types,
                                                              exclude_keys=exclude_keys)
                class_parameters = update_dictionary_with_subdictionary(class_parameters, class_parameters_from_file)
                self.custom_classes[section] = custom_class(**class_parameters)
                self.typed_sections[section] = class_parameters

            elif 'function' in config_file_items.keys():
                # import a module defined elsewhere
                module = importlib.import_module(config_file_items['module'])
                # import a function from a module
                custom_function = getattr(module, config_file_items['function'])
                function_parameters, types = get_default_args_types_from_function(custom_function)
                function_parameters_from_file = self.apply_types(config_file_items, types,
                                                                 exclude_keys=exclude_keys)

                if 'custom_return' in config_file_items.keys():
                    # custom_return means that this function is just being called to return something else
                    self.custom_function_return[config_file_items['custom_return']] = \
                        custom_function(**function_parameters_from_file)
                    self.typed_sections[section] = function_parameters_from_file
                    continue
                elif 'signal_return' in config_file_items.keys():
                    self.signal_creating_functions[section] = custom_function
                    self.signal_creating_function_parameters[section] = update_dictionary_with_subdictionary(
                        function_parameters,
                        function_parameters_from_file)
                elif 'pta_return' in config_file_items.keys():
                    self.pta_creating_functions[section] = custom_function
                    self.pta_creating_function_parameters[section] = update_dictionary_with_subdictionary(
                        function_parameters,
                        function_parameters_from_file)
                else:
                    raise (AttributeError((
                        "'function' needs one of 'custom_return=SOMETHING' "
                        "'signal_function=True' 'pta_function=True' "
                        "in .ini file")))
            else:
                try:
                    """
                    Get default values for models held in enterprise_extensions
                    """
                    model_function = getattr(enterprise_extensions.models, section)
                    self.pta_creating_function_parameters[section], types = get_default_args_types_from_function(
                        model_function)
                    # Update default args with those held inside of path
                    function_parameters_from_file = self.apply_types(config_file_items, types)
                    self.pta_creating_function_parameters[section] = \
                        update_dictionary_with_subdictionary(self.pta_creating_function_parameters[section],
                                                             function_parameters_from_file)
                    self.pta_creating_functions[section] = model_function
                except AttributeError as e:
                    # TODO this should probably exit
                    print(e)
                    print(f"WARNING! there is no {section} in enterprise_extensions.models")
                    raise AttributeError

    def apply_types(self, dictionary, type_dictionary, exclude_keys=[]):
        """
        Given dictionary (usually created from config_file) and dictionary containing types
        apply type to dictionary

        if CUSTOM_CLASS:your_class is in dictionary[key],
            instead of applying type it assigns from self.custom_classes
        if CUSTOM_RETURN:whatever is in dictionary[key]
            instead of applying type it assigns from self.custom_returns[whatever]
        if FUNCTION_CALL:whatever
            will call eval("whatever") and assign that
        """
        out_dictionary = {}
        for key, value in dictionary.items():
            if key in exclude_keys:
                continue
            if 'CUSTOM_FUNCTION_RETURN:' in value:
                # Apply custom class instance stored in custom_classes
                out_dictionary[key] = self.custom_function_return[value.replace('CUSTOM_FUNCTION_RETURN:', '')]
                continue
            if 'CUSTOM_CLASS:' in value:
                # Apply custom class instance stored in custom_classes
                out_dictionary[key] = self.custom_classes[value.replace('CUSTOM_CLASS:', '')]
                continue
            if 'FUNCTION_CALL:' in value:
                function_call = value.replace('FUNCTION_CALL:', '')
                out_dictionary[key] = eval(function_call)
                continue
            if key not in type_dictionary.keys():
                print(f"WARNING! {key} is not within type dictionary!")
                print(f"Object value is {value} and type is {type(value)}")
                print(f"Continuing")
                continue
            # special comprehension for (1d) numpy arrays
            if type_dictionary[key] == np.ndarray:
                out_dictionary[key] = np.array([np.float(x) for x in value.split(',')])
            # Special comprehension for bool because otherwise bool('False') == True
            elif type_dictionary[key] == bool:
                out_dictionary[key] = dictionary[key].lower().capitalize() == "True"
            else:
                out_dictionary[key] = type_dictionary[key](value)

        return out_dictionary

    def update_from_dict(self, **kwargs):
        ann = getattr(self, "__annotations__", {})
        for name, dtype in ann.items():
            if name in kwargs:
                try:
                    kwargs[name] = dtype(kwargs[name])
                except TypeError:
                    pass
                setattr(self, name, kwargs[name])

    def load_pickled_pulsars(self):
        """
        Set self.psrs and self.noise_dict
        """

        try:
            self.psrs = pickle.load(open(self.pulsar_pickle, 'rb'))
            self.noise_dict = json.load(open(self.noise_dict_json))
        except FileNotFoundError as e:
            print(e)
            exit(1)

        for par in list(self.noise_dict.keys()):
            if 'log10_ecorr' in par and 'basis_ecorr' not in par:
                ecorr = par.split('_')[0] + '_basis_ecorr_' + '_'.join(par.split('_')[1:])
                self.noise_dict[ecorr] = self.noise_dict[par]

        # assign noisedict to all enterprise models
        for key in self.pta_creating_function_parameters.keys():
            if 'noisedict' in self.pta_creating_function_parameters[key].keys():
                self.pta_creating_function_parameters[key]['noisedict'] = self.noise_dict

    def create_pta_object_from_signals(self):
        """
        Using both signals from pta objects and signals from self.signal_creating_functions
        Create a pta object
        """

        pta_list = self.get_pta_objects()
        signal_collections = [self.get_signal_collection_from_pta_object(pta) for pta in pta_list]
        for key, func in self.signal_creating_functions.items():
            signal_collections.append(func(**self.signal_creating_function_parameters[key]))

        signal_collection = sum(signal_collections[1:], signal_collections[0])

        model_list = [signal_collection(psr) for psr in self.psrs]
        pta = signal_base.PTA(model_list)

        # apply noise dictionary to pta
        pta.set_default_params(self.noise_dict)
        # return pta object
        return pta

    def get_signal_collection_from_pta_object(self, pta):
        """
        Under assumption that same model has been applied to ALL pulsars
        and that there are pulsars inside of this pta,
        get signal collection from this pta object
        """
        return type(pta.pulsarmodels[0])



    def get_pta_objects(self):
        """
        Using pta creating functions specified in config, get list of pta objects
        """
        pta_list = []
        if len(self.psrs) == 0:
            print("Loading pulsars")
            self.load_pickled_pulsars()

        for key in self.pta_creating_function_parameters.keys():
            pta_list.append(self.pta_creating_functions[key](psrs=self.psrs,
                                                             **self.pta_creating_function_parameters[key]))
        return pta_list
